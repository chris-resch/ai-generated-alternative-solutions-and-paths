"""Solution-path measures at file level, per run and per task, from the step paths in path.jsonl.

Each step in path.jsonl is [action letter, target classes, repository files, exit code]:
  action   F search, D read, E edit (any file write, incl. the agent's own scripts), R run a script,
           T run repository tests, V revert, W review own changes (git diff/status), U setup,
           M malformed call, O other, S submit
  classes  src (repository source file at the base commit), test (repository test file),
           new (file in the repository folder that is not in the base commit), outside (/tmp etc.), build
A "place" is a repository source file. Test files and scratch files are where checks happen, not places.

Per run (runs.csv):
  first_edit      step of the first edit of a source file (the final `git diff > patch.txt` step is not an edit)
  explore_steps   clean steps before first_edit (clean = not malformed, not setup)
  breadth         source files searched or read before first_edit
  revisits        reads/searches of a source file already looked at, before first_edit
  first_place     source file(s) of the first search or read that names one
  landing         source files in the final patch step (git diff <files> > patch.txt), else those of the last edit
  experiments     edits followed by a check (T or R) before the next edit
  failed_checks   checks after first_edit with a non-zero exit code
  stay / go       after a failed check, whether the next look or edit stays with files already edited or goes to
                  another source file
  human_*         relation to the files the merged solution changed (Python files only)

Per task (tasks.csv), over the runs that are not friction loops:
  start_agree     share of runs whose first place is the most common first place
  gate_file/share most common source file read before the first edit by solving runs, and the share that read it;
                  gate_files = all files read before the first edit by at least 90% of solving runs
  spread          mean over run pairs of 1 - Jaccard overlap of the files read before the first edit
  split_point     median over run pairs of how many places two runs share, in the same order, before they differ
  crossroad_*     the place after which runs went the most different ways (next place, or editing it), with the
                  number of different moves
  misdirected     share of solving runs whose first place contains none of their landing files
  human_start, human_seen, human_landing, seen_not_taken   shares relating paths to the merged solution

Reliability (reliability.csv): each measure computed separately in two disjoint groups of five models, then
correlated across tasks (Spearman). Run-level measures are taken net of each model's own average first
(log(1 + x) minus the model mean), because models differ in style far more than tasks do.

Usage:
    python scripts/pilot/path_measures.py --paths data/derived/*/path.jsonl \
        --results data/public/verdicts/*/per_instance_details.json \
        --human results/pilot/fix_files.jsonl --out results/pilot/paths
"""
import argparse, csv, json, math, os, re, statistics as st
from collections import Counter, defaultdict
from itertools import combinations

TEST_RE = re.compile(r'(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_tests?\.py$|conftest\.py$')
GROUP_A = ['claude-4-5-opus-high', 'claude-4-5-haiku-high', 'gpt-5-2-high', 'kimi-k2-5-high', 'deepseek-3-2-high']
GROUP_B = ['claude-4-6-opus', 'claude-4-5-sonnet-high', 'gpt-5-mini', 'gemini-3-flash-high', 'minimax-2-5-high']
# glm-5-high is in neither group: 14% of its calls are malformed.


def classes(step):
    return set(step[1].split('|')) - {''}


def src_files(step):
    return {f for f in step[2] if not TEST_RE.search(f)}


def patch_step(steps):
    """Index of the final patch-writing step: the last E or V step, if it is an E that names a new file."""
    for k in range(len(steps) - 1, -1, -1):
        if steps[k][0] in 'EV':
            return k if steps[k][0] == 'E' and 'new' in classes(steps[k]) else None
    return None


def friction_loop(steps):
    acts = [s[0] for s in steps]
    longest, cur = 0, 0
    for a in acts:
        cur = cur + 1 if a == 'M' else 0
        longest = max(longest, cur)
    return longest >= 20 or (acts and acts.count('M') / len(acts) > 0.5)


def run_measures(steps, human):
    ps = patch_step(steps)
    edits = [k for k, s in enumerate(steps) if s[0] == 'E' and src_files(s) and k != ps]
    first = edits[0] if edits else None
    end = first if first is not None else len(steps)
    looks = [(k, src_files(s)) for k, s in enumerate(steps) if s[0] in 'FD' and src_files(s)]
    seen, seq, revisits = set(), [], 0
    for k, fs in looks:
        if k >= end:
            break
        for f in sorted(fs):
            if f in seen:
                revisits += 1
            else:
                seen.add(f); seq.append(f)
    first_place = looks[0][1] if looks else set()
    edited = set().union(*(src_files(steps[k]) for k in edits)) if edits else set()
    submits = [k for k, s in enumerate(steps) if s[0] == 'S']
    if ps is not None and src_files(steps[ps]):
        landing, landing_rule = src_files(steps[ps]), 'patch step'
    elif submits and src_files(steps[submits[-1]]):
        # some runs write the diff in the submit command itself (`git diff <files>`), with no separate patch step
        landing, landing_rule = src_files(steps[submits[-1]]), 'submit step'
    elif edits:
        landing, landing_rule = src_files(steps[edits[-1]]), 'last edit'
    else:
        landing, landing_rule = set(), 'none'
    m = dict(n_steps=len(steps), clean_steps=sum(s[0] not in 'MU' for s in steps),
             first_edit=first, landing_rule=landing_rule, friction_loop=friction_loop(steps),
             reverts=sum(s[0] == 'V' for s in steps))
    if first is None:
        m.update(explore_steps=None, breadth=None, revisits=None)
    else:
        m.update(explore_steps=sum(s[0] not in 'MU' for s in steps[:first]), breadth=len(seen), revisits=revisits)
    # checks after the first edit, and where the run goes after a failed check
    experiments = failed = stay = go = 0
    if first is not None:
        edit_set = set(edits)
        for i, k in enumerate(edits):
            nxt = edits[i + 1] if i + 1 < len(edits) else len(steps)
            if any(steps[j][0] in 'TR' for j in range(k + 1, nxt)):
                experiments += 1
        for k in range(first + 1, len(steps)):
            a, rc = steps[k][0], steps[k][3]
            if a in 'TR' and rc not in (0, None):
                failed += 1
                done = set().union(*(src_files(steps[e]) for e in edits if e <= k))
                for j in range(k + 1, len(steps)):
                    if j == ps:
                        break
                    s = steps[j]
                    if s[0] in 'FDE' and src_files(s):
                        if src_files(s) <= done:
                            stay += 1
                        else:
                            go += 1
                        break
    m.update(experiments=experiments, failed_checks=failed, stay=stay, go=go)
    m.update(human_start=bool(first_place & human) if first_place else None,
             human_seen=bool(seen & human) if first is not None else None,
             human_edited=bool(edited & human), human_landing=bool(landing & human) if landing else None)
    m['_first_place'], m['_seen'], m['_seq'], m['_landing'], m['_edited'] = first_place, seen, seq, landing, edited
    m['_looks'] = [(k, fs) for k, fs in looks] + [(k, src_files(steps[k])) for k in edits]
    m['_edits'] = set(edits)
    return m


def next_moves(m):
    """For each place a run visits, what it does next: another place, editing that place, or nothing."""
    events = sorted(m['_looks'], key=lambda x: x[0])
    edit_steps = m['_edits']
    out, done = {}, set()
    for i, (k, fs) in enumerate(events):
        for f in sorted(fs):
            if f in done or k in edit_steps:
                continue
            done.add(f)
            move = 'nothing'
            for k2, fs2 in events[i + 1:]:
                if k2 in edit_steps and f in fs2:
                    move = 'edit here'; break
                other = sorted(fs2 - {f})
                if other:
                    move = other[0]; break
            out[f] = move
    return out


def jaccard_dist(a, b):
    return 1 - len(a & b) / len(a | b) if a | b else 0.0


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def task_measures(runs):
    """runs: list of (model, resolved, m). Friction loops are dropped before this is called."""
    t = dict(n_runs=len(runs), n_resolved=sum(r for _, r, _ in runs))
    firsts = [m['_first_place'] for _, _, m in runs if m['_first_place']]
    if firsts:
        top, n = Counter(f for fp in firsts for f in fp).most_common(1)[0]
        t.update(first_file=top, start_agree=sum(top in fp for fp in firsts) / len(firsts))
    edited_runs = [(mod, r, m) for mod, r, m in runs if m['first_edit'] is not None]
    solved = [m for _, r, m in edited_runs if r]
    if solved:
        c = Counter(f for m in solved for f in m['_seen'])
        if c:
            gate, n = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0]     # ties broken by name
            t.update(gate_file=gate, gate_share=n / len(solved),
                     gate_files='|'.join(sorted(f for f, k in c.items() if k >= 0.9 * len(solved))))
        mis = [not (m['_first_place'] & m['_landing']) for m in solved if m['_first_place'] and m['_landing']]
        t['misdirected'] = sum(mis) / len(mis) if mis else None
    pairs = list(combinations([m for _, _, m in edited_runs], 2))
    if pairs:
        t['spread'] = st.mean(jaccard_dist(a['_seen'], b['_seen']) for a, b in pairs)
        t['split_point'] = st.median(common_prefix(a['_seq'], b['_seq']) for a, b in pairs)
    moves = defaultdict(list)
    for _, _, m in runs:
        for f, mv in next_moves(m).items():
            moves[f].append(mv)
    cands = [(len(set(v)), len(v), f) for f, v in moves.items() if len(v) >= 3]
    if cands:
        nb, nr, f = max(cands)
        t.update(crossroad_file=f, crossroad_runs=nr, crossroad_moves=nb,
                 crossroad_top_share=Counter(moves[f]).most_common(1)[0][1] / nr)
    for key in ('human_start', 'human_seen', 'human_landing'):
        vals = [m[key] for _, r, m in runs if m[key] is not None and (r or key == 'human_start')]
        t[key] = sum(vals) / len(vals) if vals else None
    away = [m for m in solved if m['_landing'] and m['human_landing'] is False]
    t['n_landing_away'] = len(away)
    t['seen_not_taken'] = sum(bool(m['human_seen']) for m in away) / len(away) if away else None
    return t


def spearman(x, y):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry = ranks(x), ranks(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float('nan')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--paths', nargs='+', required=True)
    ap.add_argument('--results', nargs='+', required=True, help='per_instance_details.json, one per run folder')
    ap.add_argument('--human', required=True, help="the files of the merged solutions, from fix_files.py")
    ap.add_argument('--out', required=True)
    a = ap.parse_args()

    resolved = {}
    for p in a.results:
        model = p.split('20260217_mini-v2.0.0_')[1].split('/')[0]
        resolved[model] = {k: v.get('resolved', False) for k, v in json.load(open(p)).items()}
    human, unreliable = {}, set()
    for line in open(a.human):
        h = json.loads(line)
        py = {f for f in h.get('fix_files', []) if f.endswith('.py')}
        human[h['instance_id']] = py
        if len(h.get('fix_files', [])) > 10:      # pull request diff far larger than a fix: base not found
            unreliable.add(h['instance_id'])

    by_task = defaultdict(list)
    os.makedirs(a.out, exist_ok=True)
    run_cols = ['instance_id', 'model', 'resolved', 'n_steps', 'clean_steps', 'friction_loop', 'first_edit',
                'explore_steps', 'breadth', 'revisits', 'experiments', 'failed_checks', 'stay', 'go', 'reverts',
                'landing_rule', 'human_start', 'human_seen', 'human_edited', 'human_landing',
                'first_place', 'landing']
    with open(os.path.join(a.out, 'runs.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=run_cols)
        w.writeheader()
        for p in a.paths:
            for line in open(p):
                r = json.loads(line)
                iid, model = r['instance_id'], r['run']
                hum = set() if iid in unreliable else human.get(iid, set())
                m = run_measures(r['steps'], hum)
                res = bool(resolved[model].get(iid, False))
                if not hum:
                    for k in ('human_start', 'human_seen', 'human_edited', 'human_landing'):
                        m[k] = None
                row = {k: m.get(k) for k in run_cols if k in m}
                row.update(instance_id=iid, model=model, resolved=res,
                           first_place='|'.join(sorted(m['_first_place'])), landing='|'.join(sorted(m['_landing'])))
                w.writerow(row)
                by_task[iid].append((model, res, m))

    task_rows, rel = [], defaultdict(lambda: ([], []))
    go_share = ([], [])          # moving elsewhere after a failed check, per group (A, B)
    run_keys = ['explore_steps', 'breadth', 'revisits', 'experiments', 'failed_checks']
    # model means of log(1 + x) for netting out style
    mm = defaultdict(list)
    for iid, runs in by_task.items():
        for model, res, m in runs:
            if not m['friction_loop']:
                for k in run_keys:
                    if m.get(k) is not None:
                        mm[(model, k)].append(math.log1p(m[k]))
    mmean = {k: st.mean(v) for k, v in mm.items()}
    for iid, runs in sorted(by_task.items()):
        ok = [x for x in runs if not x[2]['friction_loop']]
        t = task_measures(ok)
        t['instance_id'] = iid
        t['n_models_failing'] = sum(not r for _, r, _ in runs)
        t['human_files'] = '|'.join(sorted(human.get(iid, set()))) if iid not in unreliable else 'unreliable'
        for k in run_keys:
            v = [math.log1p(m[k]) - mmean[(mod, k)] for mod, _, m in ok if m.get(k) is not None]
            t[k + '_net'] = st.mean(v) if v else None
        task_rows.append(t)
        for grp_name, grp in (('A', GROUP_A), ('B', GROUP_B)):
            g = [x for x in ok if x[0] in grp]
            if len(g) < 3:
                continue
            tg = task_measures(g)
            for k in run_keys:
                v = [math.log1p(m[k]) - mmean[(mod, k)] for mod, _, m in g if m.get(k) is not None]
                tg[k + '_net'] = st.mean(v) if len(v) >= 3 else None
            for k in ('explore_steps_net', 'breadth_net', 'revisits_net', 'experiments_net', 'failed_checks_net',
                      'start_agree', 'gate_share', 'spread', 'split_point', 'crossroad_moves', 'misdirected',
                      'human_start', 'human_seen'):
                if tg.get(k) is not None:
                    rel[k][0 if grp_name == 'A' else 1].append((iid, tg[k]))
            # Moving elsewhere after a failed check: of all moves that follow a failed check in this
            # group's runs (stay + go, see run_measures), the share that goes to another source file. Only when the
            # group made at least 3 such moves on the task.
            moves = sum((m.get('stay') or 0) + (m.get('go') or 0) for _, _, m in g)
            if moves >= 3:
                go_share[0 if grp_name == 'A' else 1].append((iid, sum(m.get('go') or 0 for _, _, m in g) / moves))

    cols = ['instance_id', 'n_runs', 'n_resolved', 'n_models_failing', 'human_files', 'first_file', 'start_agree',
            'gate_file', 'gate_share', 'gate_files', 'spread', 'split_point', 'crossroad_file', 'crossroad_runs',
            'crossroad_moves', 'crossroad_top_share', 'misdirected', 'human_start', 'human_seen', 'human_landing',
            'n_landing_away', 'seen_not_taken'] + [k + '_net' for k in run_keys]
    with open(os.path.join(a.out, 'tasks.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for t in task_rows:
            w.writerow(t)

    with open(os.path.join(a.out, 'reliability.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['measure', 'tasks', 'spearman_A_vs_B', 'spearman_brown_full_panel'])
        for k, (ga, gb) in rel.items():
            da, db = dict(ga), dict(gb)
            both = sorted(set(da) & set(db))
            if len(both) < 20:
                continue
            r = spearman([da[i] for i in both], [db[i] for i in both])
            w.writerow([k, len(both), round(r, 3), round(2 * r / (1 + r), 3) if r > -1 else ''])

        # Further rows: moving elsewhere after a failed check, and two measures within Django (231 of the 500 tasks)
        # and within the other 11 repositories.
        def extra_row(name, ga, gb, keep=lambda iid: True):
            da, db = dict(ga), dict(gb)
            both = sorted(i for i in set(da) & set(db) if keep(i))
            r = spearman([da[i] for i in both], [db[i] for i in both])
            w.writerow([name, len(both), round(r, 3), round(2 * r / (1 + r), 3) if r > -1 else ''])
        extra_row('go_share', *go_share)
        for k in ('explore_steps_net', 'human_start'):
            extra_row(k + ' (django only)', *rel[k], keep=lambda iid: iid.startswith('django__'))
            extra_row(k + ' (other repositories)', *rel[k], keep=lambda iid: not iid.startswith('django__'))


if __name__ == '__main__':
    main()
