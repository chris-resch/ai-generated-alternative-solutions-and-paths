"""Parse mini-SWE-agent trajectories (tool-calling format, v2) into analysis tables.

One run = one folder of <instance_id>/<instance_id>.traj.json files (as published for the
SWE-bench bash-only runs). Standard library only.

Usage:
    python scripts/pilot/parse_trajs.py data/public/glm-5-high/trajs --run glm-5-high --out data/derived/glm-5-high

Writes to --out:
    steps.csv     one row per bash command: step, category, files opened/edited, return code, output size
    instances.csv one row per task: exit status, counts per category, friction, read set, edit loci, patch size
    patches.jsonl one line per task: parsed final patch (files, hunks with old-file line ranges)
"""
import argparse, csv, json, os, re, sys
from collections import Counter

ROOT = '/testbed'
FILE_EXT = r'(?:py|pyx|pxd|pyi|rst|txt|cfg|toml|ini|c|h|js|html|json|yaml|yml|po|css|tex|in)'
PATH_RE = re.compile(r'(?<![\w/.-])((?:/testbed/|\./)?[\w.-]+(?:/[\w.-]+)*\.' + FILE_EXT + r')\b')
# paths shown in search output: grep hits ("path:12:") or bare paths, one per line (find, ls -R)
GREP_HIT_RE = re.compile(r'^(?:/testbed/|\./)?([\w.-]+(?:/[\w.-]+)*\.' + FILE_EXT + r')(?::\d+[:-]|[ \t]*$)', re.M)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\b", re.S)
SUBMIT_MARK = 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'
LOOP_MIN = 20  # identical consecutive commands that mark a stuck run

# Order = priority when one command chains several actions (e.g. "edit && run test" counts as edit).
CATEGORIES = ['submit', 'edit', 'revert', 'test', 'setup', 'run', 'read', 'search', 'review', 'other']
PATTERNS = {
    'submit': re.compile(SUBMIT_MARK),
    'revert': re.compile(r'\bgit\s+(checkout\s+(--\s|\S+\s+--|[\w./-]+\.\w+\b)|restore\b|stash\b|reset\b)'),
    'edit':   re.compile(r'\bsed\s+(-\w*\s+)*-i|\bperl\s+-\w*i|\bcat\s+>>?|\btee\b|(?<![0-9&])>>?\s*[\w./-]+\.\w+|\bgit\s+apply\b|\bpatch\s+-p|\bed\s|\bmv\s|\bcp\s|\brm\s|\btouch\s'),
    'test':   re.compile(r'\bpytest\b|runtests\.py|\bbin/test\b|-m\s+unittest|\btox\b|setup\.py\s+test|manage\.py\s+test'),
    'setup':  re.compile(r'\b(pip3?|conda|uv)\s+install\b|setup\.py\s+(build|build_ext|develop|install|egg_info)\b|\bcython\b'),
    'run':    re.compile(r'\bpython[\d.]*\b'),
    'read':   re.compile(r'^\s*(cat|head|tail|nl|less|more|sed\s+-n|awk|wc|view)\b'),
    'search': re.compile(r'^\s*(grep|egrep|rg|ag|find|ls|tree|git\s+(grep|log|show|blame))\b|\|\s*grep\b'),
    'review': re.compile(r'^\s*git\s+(diff|status)\b'),
}
PY_WRITE_RE = re.compile(r"open\([^)]*['\"][wa]\+?['\"]|\.write_text\(|\.write\(")


def norm_path(p, cwd):
    """Map a path seen in a command to a repository-relative path."""
    if p.startswith(ROOT + '/'):
        return p[len(ROOT) + 1:]
    if p.startswith('/'):
        return None  # outside the repository (/tmp, site-packages, ...)
    p = p[2:] if p.startswith('./') else p
    p = p[len('testbed/'):] if p.startswith('testbed/') else p
    return os.path.normpath(os.path.join(cwd, p)) if cwd else p


def split_segments(cmd):
    """Split a shell command on &&, ||, ; and newlines (heredoc bodies removed first)."""
    return [s.strip() for s in re.split(r'&&|\|\||;|\n', cmd) if s.strip()]


def classify(cmd):
    """Return (primary category, all categories, opened files, edited files) for one bash command."""
    bodies = [m.group(3) for m in HEREDOC_RE.finditer(cmd)]
    head = HEREDOC_RE.sub('<<HEREDOC', cmd)  # keep the command line, drop heredoc bodies
    cats, opened, edited = set(), set(), set()
    cwd = ''
    for seg in split_segments(head):
        m = re.match(r'cd\s+(\S+)$', seg)
        if m:
            d = m.group(1)
            if d.startswith('/') and not d.startswith(ROOT):
                cwd = None  # outside the repository (/tmp, ...): relative paths are not repo files
            else:
                cwd = norm_path(d, '') if d.startswith(ROOT) else d
                cwd = '' if cwd in ('.', ROOT, None) else cwd
            continue
        if re.match(r'echo\b', seg) and SUBMIT_MARK not in seg and '>' not in seg:
            continue
        seg_cats = {c for c in CATEGORIES[:-1] if PATTERNS[c].search(seg)}
        if 'python' in seg and 'HEREDOC' in seg and any(PY_WRITE_RE.search(b) for b in bodies):
            seg_cats.add('edit')
        seg_cats = seg_cats or {'other'}
        cats |= seg_cats
        paths = set() if cwd is None and not re.search(r'/testbed/', seg) else \
            {norm_path(p, cwd or '') for p in PATH_RE.findall(seg) if cwd is not None or p.startswith(ROOT)} - {None}
        if seg_cats & {'edit', 'revert'}:
            edited |= paths
        elif seg_cats & {'read', 'search', 'test', 'run', 'review'}:
            opened |= paths
    if 'edit' in cats:  # files written from python heredocs, e.g. open('x.py','w')
        for b in bodies:
            if PY_WRITE_RE.search(b):
                edited |= {norm_path(p, cwd or '') for p in PATH_RE.findall(b) if cwd is not None or p.startswith(ROOT)} - {None}
    primary = next(c for c in CATEGORIES if c in cats) if cats else 'other'
    return primary, sorted(cats), sorted(opened), sorted(edited)


def parse_output(content):
    rc = re.search(r'<returncode>(-?\d+)</returncode>', content or '')
    return (int(rc.group(1)) if rc else None), len(content or '')


def parse_patch(diff):
    """Files and hunks of a unified diff; hunk ranges refer to the base commit (old side)."""
    files, cur = [], None
    for line in (diff or '').splitlines():
        m = re.match(r'diff --git a/(\S+) b/(\S+)', line)
        if m:
            cur = dict(path=m.group(2), new_file=False, deleted=False, added=0, removed=0, hunks=[])
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith('new file mode'):
            cur['new_file'] = True
        elif line.startswith('deleted file mode'):
            cur['deleted'] = True
        elif line.startswith('@@'):
            h = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)', line)
            if h:
                start, length = int(h.group(1)), int(h.group(2) or 1)
                cur['hunks'].append(dict(old_start=start, old_len=length, context=h.group(5).strip()))
        elif line.startswith('+') and not line.startswith('+++'):
            cur['added'] += 1
        elif line.startswith('-') and not line.startswith('---'):
            cur['removed'] += 1
    for f in files:
        p = f['path']
        f['is_test'] = bool(re.search(r'(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_tests?\.py$|conftest\.py$', p))
        # new top-level files such as reproduce.py are the agent's scratch work, not part of the patch
        f['is_scratch'] = f['new_file'] and ('/' not in p or bool(re.search(r'(reproduce|repro|debug|scratch)', p, re.I)))
    return files


def longest_repeat(steps):
    """Longest run of identical consecutive commands (a stuck loop)."""
    best = cur = 1 if steps else 0
    for a, b in zip(steps, steps[1:]):
        cur = cur + 1 if a['command'] == b['command'] else 1
        best = max(best, cur)
    return best


def parse_traj(path, run):
    d = json.load(open(path))
    iid = d.get('instance_id') or os.path.basename(path).split('.')[0]
    info = d.get('info', {})
    steps, outputs = [], {}
    turn = 0
    def add_call(k, call_id, arguments):
        try:
            cmd = json.loads(arguments).get('command', '')
        except (AttributeError, TypeError, json.JSONDecodeError):
            cmd = None  # malformed call: kept as friction
        steps.append(dict(run=run, instance_id=iid, turn=turn, call=k, call_id=call_id, command=cmd))

    rc_extra = {}
    for m in d.get('messages', []):
        role = m.get('role')
        # Chat Completions format (most models): assistant message with tool_calls, answered by role 'tool'
        if role == 'assistant':
            turn += 1
            for k, tc in enumerate(m.get('tool_calls') or []):
                add_call(k, tc.get('id'), (tc.get('function') or {}).get('arguments'))
        elif role == 'tool':
            outputs[m.get('tool_call_id')] = m.get('content')
        # Responses format (OpenAI GPT-5 runs): a 'response' object whose output list holds
        # function_call items, answered by separate function_call_output messages (linked by call_id)
        elif m.get('object') == 'response':
            turn += 1
            calls = [o for o in m.get('output') or [] if o.get('type') == 'function_call']
            for k, o in enumerate(calls):
                add_call(k, o.get('call_id'), o.get('arguments'))
        elif m.get('type') == 'function_call_output':
            out = m.get('output')
            outputs[m.get('call_id')] = out if isinstance(out, str) else json.dumps(out)
            rc_extra[m.get('call_id')] = (m.get('extra') or {}).get('returncode')
    for s in steps:
        rc, size = parse_output(outputs.get(s['call_id']))
        if rc is None and rc_extra.get(s['call_id']) is not None:
            rc = int(rc_extra[s['call_id']])
        s['returncode'], s['output_chars'] = rc, size
        # malformed: arguments not valid JSON, or a 'command' that is a JSON fragment such as [[0], ...]
        # (seen with GLM-5: such calls run as shell commands and fail with return code 127)
        if s['command'] is None or s['command'].lstrip()[:1] in ('[', '{'):
            s.update(category='malformed', categories='malformed', opened='', edited='')
        else:
            primary, cats, opened, edited = classify(s['command'])
            s.update(category=primary, categories='|'.join(cats), opened='|'.join(opened), edited='|'.join(edited))
        s['surfaced'] = '|'.join(sorted(set(GREP_HIT_RE.findall(outputs.get(s['call_id']) or '')))) \
            if s['category'] in ('search', 'read') else ''
    patch_files = parse_patch(info.get('submission'))
    fix_files = [f for f in patch_files if not f['is_scratch']]
    counts = Counter(s['category'] for s in steps)
    opened = sorted({p for s in steps for p in s['opened'].split('|') if p})
    surfaced = sorted({p for s in steps for p in s['surfaced'].split('|') if p})
    inst = dict(
        run=run, instance_id=iid, repo=iid.split('__')[0], exit_status=info.get('exit_status'),
        mini_version=info.get('mini_version'), api_calls=(info.get('model_stats') or {}).get('api_calls'),
        turns=turn, commands=len(steps), parallel_turns=sum(1 for t, c in Counter(s['turn'] for s in steps).items() if c > 1),
        **{f'n_{c}': counts.get(c, 0) for c in CATEGORIES + ['malformed']},
        nonzero_returncodes=sum(1 for s in steps if s['returncode'] not in (0, None)),
        missing_outputs=sum(1 for s in steps if s['returncode'] is None and s['category'] != 'submit'),
        longest_repeat=longest_repeat(steps),
        share_malformed=round(counts.get('malformed', 0) / len(steps), 3) if steps else 0,
        edit_test_cycles=sum(1 for a, b in zip(steps, steps[1:]) if a['category'] == 'edit' and b['category'] in ('test', 'run')),
        n_opened=len(opened), n_surfaced=len(surfaced), opened=';'.join(opened),
        patch_files=';'.join(f['path'] for f in fix_files if not f['is_test']),
        patch_test_files=';'.join(f['path'] for f in fix_files if f['is_test']),
        scratch_files=';'.join(f['path'] for f in patch_files if f['is_scratch']),
        patch_added=sum(f['added'] for f in fix_files), patch_removed=sum(f['removed'] for f in fix_files),
        patch_hunks=sum(len(f['hunks']) for f in fix_files), empty_patch=not fix_files,
    )
    # failure mode, so that unsolved-because-stuck is not read as unsolved-because-hard
    if inst['exit_status'] == 'Submitted':
        inst['failure_mode'] = 'submitted'
    elif inst['longest_repeat'] >= LOOP_MIN or inst['share_malformed'] > 0.5:
        inst['failure_mode'] = 'friction_loop'
    else:
        inst['failure_mode'] = 'limit_or_error'
    return steps, inst, dict(run=run, instance_id=iid, files=patch_files, submission=info.get('submission'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('trajs', help='folder with <instance_id>/<instance_id>.traj.json')
    ap.add_argument('--run', required=True, help='short run name written into every row')
    ap.add_argument('--out', required=True, help='output folder')
    ap.add_argument('--limit', type=int, help='parse only the first N tasks (for testing)')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    paths = sorted(os.path.join(a.trajs, d, f) for d in os.listdir(a.trajs) if os.path.isdir(os.path.join(a.trajs, d))
                   for f in os.listdir(os.path.join(a.trajs, d)) if f.endswith('.traj.json'))[:a.limit]
    preds_path = os.path.join(a.trajs, 'preds.json')
    preds = json.load(open(preds_path)) if os.path.exists(preds_path) else {}
    all_steps, insts, mismatches, failed = [], [], 0, []
    with open(os.path.join(a.out, 'patches.jsonl'), 'w') as pf:
        for p in paths:
            try:
                steps, inst, patch = parse_traj(p, a.run)
            except Exception as e:  # keep going; report at the end
                failed.append((p, repr(e)))
                continue
            pred = (preds.get(inst['instance_id']) or {}).get('model_patch')
            inst['matches_preds'] = None if pred is None else (pred.strip() == (patch['submission'] or '').strip())
            mismatches += inst['matches_preds'] is False
            all_steps += steps
            insts.append(inst)
            pf.write(json.dumps(patch) + '\n')
    for name, rows, drop in (('steps.csv', all_steps, {'call_id'}), ('instances.csv', insts, set())):
        if rows:
            with open(os.path.join(a.out, name), 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=[k for k in rows[0] if k not in drop], extrasaction='ignore')
                w.writeheader()
                w.writerows(rows)
    cats = Counter(s['category'] for s in all_steps)
    print(f'parsed {len(insts)} of {len(paths)} trajectories, {len(all_steps)} commands')
    print('commands by category:', dict(cats.most_common()))
    print('exit status:', dict(Counter(i['exit_status'] for i in insts)))
    print('failure mode:', dict(Counter(i['failure_mode'] for i in insts)))
    print(f"tasks with malformed calls: {sum(i['n_malformed'] > 0 for i in insts)}; malformed calls in total: {cats.get('malformed', 0)}")
    print(f"empty patches: {sum(i['empty_patch'] for i in insts)}; patch differs from preds.json: {mismatches}")
    for p, e in failed[:5]:
        print('FAILED', p, e, file=sys.stderr)


if __name__ == '__main__':
    main()
