"""Files each merged solution changes, read from the SWE-bench Verified dataset (field `patch`).

path_measures.py reads this file to know which files the merged solution of each issue changed.

Usage:
    python scripts/pilot/fix_files.py --verified data/public/swebench_verified.jsonl --out results/pilot/fix_files.jsonl

Writes one line per task: {"instance_id", "fix_files", "test_files"}, the fields path_measures.py reads. A file
counts as a test file by the pattern TEST_RE below (the same as in path_measures.py); the dataset keeps the tests in
a separate field, so test_files is normally empty.
"""
import argparse, json, os, re

TEST_RE = re.compile(r'(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_tests?\.py$|conftest\.py$')

DIFF_FILE = re.compile(r'^diff --git a/(\S+) b/', re.M)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verified', required=True, help='SWE-bench Verified rows, one JSON object per line')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    n = one_py = 0
    with open(a.out, 'w') as out:
        for line in open(a.verified):
            row = json.loads(line)
            files = sorted(set(DIFF_FILE.findall(row['patch'])))
            fix = [f for f in files if not TEST_RE.search(f)]
            test = [f for f in files if TEST_RE.search(f)]
            out.write(json.dumps({'instance_id': row['instance_id'], 'fix_files': fix, 'test_files': test}) + '\n')
            n += 1
            one_py += sum(f.endswith('.py') for f in fix) == 1
    print(f'{n} tasks; {one_py} merged solutions change exactly one Python file -> {a.out}')


if __name__ == '__main__':
    main()
