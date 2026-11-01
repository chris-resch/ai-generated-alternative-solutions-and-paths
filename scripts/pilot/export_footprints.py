"""Compact per-run footprints of the agents' steps, restricted to real repository files.

For every task, the file list of the repository at the task's base commit is read from a
blobless clone (commits and trees only, no file contents). Each run's steps.csv (from
parse_trajs.py) is then reduced to one line per task: the action sequence and the repository
files the agent opened, saw in search output, and edited, each with the step where it first
happened. Paths the agent wrote relative to a subfolder are matched to the one repository path
that ends with them; scratch files (reproduce.py, /tmp, patch.txt) drop out because they are not
in the tree.

Usage:
    python scripts/pilot/export_footprints.py --verified data/public/swebench_verified.jsonl \
        --derived data/derived --repos data/repos

Writes:
    data/repos/<owner>__<name>.git    blobless bare clones (first run only, then reused)
    data/derived/trees.jsonl          per task: number of files and of .py files at the base commit
    data/derived/<run>/footprint.jsonl  per task: sequence, opened/surfaced/edited files with first step
    data/derived/<run>/path.jsonl       per task: every step as [action letter, target classes, repository files],
                                        keeping scratch targets (new files, outside the repository, build output)
"""
import argparse, csv, json, os, re, subprocess, sys
from collections import defaultdict

csv.field_size_limit(sys.maxsize)
LETTER = {'submit': 'S', 'edit': 'E', 'revert': 'V', 'test': 'T', 'setup': 'U', 'run': 'R',
          'read': 'D', 'search': 'F', 'review': 'W', 'other': 'O', 'malformed': 'M'}

# Target classes for the step-level path (path.jsonl). Unlike the footprint, scratch targets are kept.
#   src  repository source file at the base commit     test  repository test file
#   new  file in the repository folder that is not in the base commit (agent's own scripts, tests, patch.txt)
#   outside  file outside the repository (/tmp, installed packages)   build  build output (build/, .so, generated .c)
FILE_EXT = r'(?:py|pyx|pxd|pyi|rst|txt|cfg|toml|ini|c|h|js|html|json|yaml|yml|po|css|tex|in|so|diff|patch|sh|md)'
ANY_PATH = re.compile(r'(?<![\w/.-])(/?[\w.-]+(?:/[\w.-]+)*\.' + FILE_EXT + r')\b')
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\b", re.S)
TEST_RE = re.compile(r'(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_tests?\.py$|conftest\.py$')
BUILD_RE = re.compile(r'(^|/)build/|\.so$|\.egg-info/|(^|/)dist/')


def step_targets(row, files, by_suffix):
    """Classes and repository files a step refers to, from the parsed fields and the raw command."""
    cmd = row.get('command') or ''
    bodies = ' '.join(m.group(3) for m in HEREDOC.finditer(cmd))   # scripts written via heredoc
    text = HEREDOC.sub(' ', cmd) + ' ' + bodies
    cands = set(ANY_PATH.findall(text))
    for kind in ('opened', 'edited'):
        cands |= set(filter(None, (row.get(kind) or '').split('|')))
    classes, repo = set(), set()
    for p in cands:
        if p.startswith('/') and not p.startswith('/testbed/'):
            classes.add('outside'); continue
        p = p[len('/testbed/'):] if p.startswith('/testbed/') else (p[2:] if p.startswith('./') else p)
        q = resolve(p, files, by_suffix)
        if q is not None:
            classes.add('test' if TEST_RE.search(q) else 'src'); repo.add(q)
        elif BUILD_RE.search(p) or p.endswith('.c') and resolve(p[:-2] + '.pyx', files, by_suffix):
            classes.add('build')
        else:
            classes.add('new')
    return sorted(classes), sorted(repo)


def clone(repo, repos_dir):
    path = os.path.join(repos_dir, repo.replace('/', '__') + '.git')
    if not os.path.isdir(path):
        print(f'cloning {repo} (commits and trees only) ...', flush=True)
        subprocess.run(['git', 'clone', '--quiet', '--bare', '--filter=blob:none',
                        f'https://github.com/{repo}.git', path], check=True)
    return path


def tree(git_dir, commit):
    out = subprocess.run(['git', '--git-dir', git_dir, 'ls-tree', '-r', '--name-only', commit],
                         check=True, capture_output=True, text=True).stdout
    return set(out.splitlines())


def resolve(p, files, by_suffix):
    """Map a path seen in a command to a repository path, or None."""
    if p in files:
        return p
    hits = by_suffix.get(p.split('/')[-1], ())
    hits = [f for f in hits if f.endswith('/' + p)]
    return hits[0] if len(hits) == 1 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--verified', required=True)
    ap.add_argument('--derived', required=True)
    ap.add_argument('--repos', required=True)
    ap.add_argument('--runs', nargs='*', help='only these runs (default: every folder with a steps.csv)')
    a = ap.parse_args()
    os.makedirs(a.repos, exist_ok=True)
    tasks = {r['instance_id']: r for r in map(json.loads, open(a.verified))}

    # 1. file lists at each base commit
    trees = {}
    with open(os.path.join(a.derived, 'trees.jsonl'), 'w') as tf:
        for repo in sorted({t['repo'] for t in tasks.values()}):
            git_dir = clone(repo, a.repos)
            for iid, t in sorted(tasks.items()):
                if t['repo'] != repo:
                    continue
                files = tree(git_dir, t['base_commit'])
                by_suffix = defaultdict(list)
                for f in files:
                    by_suffix[f.split('/')[-1]].append(f)
                trees[iid] = (files, by_suffix)
                tf.write(json.dumps(dict(instance_id=iid, n_files=len(files),
                                         n_py=sum(f.endswith('.py') for f in files))) + '\n')
    print(f'file lists for {len(trees)} tasks')

    # 2. one footprint line per task and run
    runs = a.runs or sorted(d for d in os.listdir(a.derived) if os.path.exists(os.path.join(a.derived, d, 'steps.csv')))
    for run in runs:
        steps = defaultdict(list)
        with open(os.path.join(a.derived, run, 'steps.csv'), newline='') as f:
            for row in csv.DictReader(f):
                steps[row['instance_id']].append(row)
        n_seen = n_resolved = n_dropped = 0
        pf = open(os.path.join(a.derived, run, 'path.jsonl'), 'w')
        with open(os.path.join(a.derived, run, 'footprint.jsonl'), 'w') as out:
            for iid, rows in sorted(steps.items()):
                files, by_suffix = trees[iid]
                first = {'opened': {}, 'surfaced': {}, 'edited': {}}
                for k, row in enumerate(rows):
                    for kind in first:
                        for p in filter(None, (row.get(kind) or '').split('|')):
                            n_seen += 1
                            q = resolve(p, files, by_suffix)
                            if q is None:
                                n_dropped += 1
                                continue
                            n_resolved += q != p
                            first[kind].setdefault(q, k)
                edits = [k for k, r in enumerate(rows) if r['category'] == 'edit']
                first_edit = edits[0] if edits else None
                out.write(json.dumps(dict(
                    instance_id=iid, run=run, n_steps=len(rows),
                    seq=''.join(LETTER.get(r['category'], 'O') for r in rows),
                    first_edit=first_edit,
                    opened=first['opened'], surfaced=first['surfaced'], edited=first['edited'],
                    opened_before_first_edit=sorted(f for f, k in first['opened'].items()
                                                    if first_edit is None or k < first_edit))) + '\n')
                path = []
                for r in rows:
                    classes, repo = step_targets(r, files, by_suffix)
                    path.append([LETTER.get(r['category'], 'O'), '|'.join(classes), repo,
                                 int(r['returncode']) if (r.get('returncode') or '').lstrip('-').isdigit() else None])
                pf.write(json.dumps(dict(instance_id=iid, run=run, steps=path)) + '\n')
        pf.close()
        print(f'{run}: {len(steps)} tasks; paths seen {n_seen}, matched by suffix {n_resolved}, '
              f'dropped as not in the repository {n_dropped}')


if __name__ == '__main__':
    main()
