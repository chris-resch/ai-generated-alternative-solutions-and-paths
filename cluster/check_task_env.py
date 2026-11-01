"""Check one task container exactly as the agent will use it, without a GPU or a model.

Run on a LOGIN node after build_task_sifs.sh (a few minutes, mostly unpacking the image):
    cd $PROJ/code       (PROJ: the project folder that holds code/ and sif/)
    .venv-agent/bin/python cluster/check_task_env.py --task django__django-11099

It loads the same settings as the agent job (mini-swe-agent's swebench.yaml, then
configs/mini/cluster-vllm.yaml), builds the environment through harness.apptainer_env, runs a few
commands through it, and prints PASS or FAIL per check:

  python    the task's conda environment is active (python from /opt/miniconda3/envs/testbed)
  base      the repository sits at the task's base commit with no changes
  future    no commits after that point are reachable (the agent cannot see the fix)
  tmp       a file written to /tmp in one command is still there in the next
  edit      a change to /testbed persists between commands, and the working-tree record sees it
  host      the project folder and the real home folder are not visible inside
  overhead  time per command (each command is a fresh `apptainer exec`)

Optional --try-limits tries Apptainer's --cpus/--memory flags (per-run CPU and memory caps); these need
cgroup support for normal users, which HPC systems often lack. The result says whether we can use them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from minisweagent.config import builtin_config_dir, get_config_from_spec  # noqa: E402
from minisweagent.run.benchmarks.swebench import get_swebench_docker_image_name  # noqa: E402
from minisweagent.utils.serialize import recursive_merge  # noqa: E402

from harness.apptainer_env import ApptainerEnvironment  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:9s} {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="django__django-11099")
    ap.add_argument("--data", default=str(REPO / "data/swebench_verified/test.jsonl"),
                    help="SWE-bench Verified rows (for the base commit)")
    ap.add_argument("--sif-dir", default=os.path.join(os.getenv("PROJ", "."), "sif", "tasks"))
    ap.add_argument("--sandbox-root", default=os.path.join(os.getenv("HPC_SCRATCH", "/tmp"), "sandboxes"))
    ap.add_argument("--config", nargs="*", default=[str(builtin_config_dir / "benchmarks" / "swebench.yaml"),
                                                     str(REPO / "configs/mini/cluster-vllm.yaml")])
    ap.add_argument("--try-limits", action="store_true")
    args = ap.parse_args()

    row = None
    with open(args.data) as f:
        for line in f:
            r = json.loads(line)
            if r["instance_id"] == args.task:
                row = r
                break
    if row is None:
        print(f"{args.task} not in {args.data}")
        return 2

    config = recursive_merge(*[get_config_from_spec(c) for c in args.config])
    env_config = {k: v for k, v in config["environment"].items() if k != "environment_class"}  # class used directly
    env_config.update(image="docker://" + get_swebench_docker_image_name(row), sif_dir=args.sif_dir,
                      sandbox_root=args.sandbox_root, record_worktree=True)
    print(f"Task {args.task}, image {env_config['image']}")

    t0 = time.time()
    env = ApptainerEnvironment(**env_config)
    print(f"Sandbox {env.sandbox_dir} built from {env.sif_path} in {time.time() - t0:.0f} s")
    print("exec flags:", " ".join(env.config.exec_args), "| workdir per run:", env.config.use_workdir)

    def run(cmd: str) -> dict:
        return env.execute({"command": cmd})

    try:
        out = run("whoami; echo HOME=$HOME; which python; python --version")
        print(out["output"].strip())
        check("python", "/envs/testbed/" in out["output"], "conda env 'testbed' active")

        # SWE-bench images end their setup with `git commit --allow-empty -am SWE-bench`
        # (swebench 4.1.0, harness/test_spec/python.py), so `git diff` shows only the agent's changes.
        # HEAD is then that commit and its parent is the task's base commit.
        out = run("echo HEAD=$(git rev-parse HEAD); echo PARENT=$(git rev-parse HEAD~1 2>/dev/null); "
                  "echo SUBJECT=$(git log -1 --format=%s HEAD); echo SETUP=$(git diff --stat HEAD~1 HEAD | tail -1); "
                  "echo DIRTY=$(git status --porcelain | wc -l)")
        val = dict(line.split("=", 1) for line in out["output"].splitlines() if "=" in line)
        head, parent = val.get("HEAD", "?"), val.get("PARENT", "")
        dirty, base = val.get("DIRTY", "?").strip(), row["base_commit"]
        if head == base:
            ok, how = True, "HEAD is the base commit"
        elif val.get("SUBJECT") == "SWE-bench" and parent == base:
            ok, how = True, (f"HEAD is SWE-bench's setup commit, its parent is the base commit {base[:10]} "
                             f"(setup changed: {val.get('SETUP') or 'nothing'})")
        else:
            ok, how = False, (f"HEAD {head[:10]} '{val.get('SUBJECT', '')}', parent {parent[:10] or '?'}, "
                              f"expected base {base[:10]}")
        check("base", ok and dirty == "0", f"{how}; {dirty} changed files")

        out = run("git rev-list --count --all --not HEAD")
        check("future", out["output"].strip() == "0", f"{out['output'].strip()} commits after HEAD")

        run("echo probe > /tmp/msa_probe")
        out = run("cat /tmp/msa_probe 2>&1")
        check("tmp", out["output"].strip() == "probe", f"second command reads: {out['output'].strip()[:60]!r}")

        first = run("f=$(git ls-files '*.py' | head -1); echo '# probe' >> \"$f\"; echo \"$f\"")
        probe_file = first["output"].strip().splitlines()[-1] if first["output"].strip() else "?"
        out = run("git diff --stat | tail -1")
        wt = first.get("extra", {}).get("worktree", {})
        check("edit", "1 file changed" in out["output"] and bool(wt.get("changed")) and probe_file in wt.get("diff", ""),
              f"{probe_file}: {out['output'].strip()}; recorded diff {len(wt.get('diff', ''))} chars")
        run(f"git checkout -- '{probe_file}'")

        # Host folders by their real names (inside the container $USER may be unset).
        user = os.getenv("USER", "")
        paths = [os.getenv("PROJ", os.getcwd()), os.getcwd()] + (
            [os.getenv("HPC_SCRATCH", f"/scratch/{user}"), f"/home/{user}"] if user else [])
        script = "; ".join(
            f"if [ -e '{p}' ]; then echo \"VISIBLE {p}: $(ls -A '{p}' 2>/dev/null | head -3 | tr '\\n' ' ')\"; fi"
            for p in paths
        )
        out = run(script + "; echo HOME=$HOME; [ -d \"$HOME\" ] && echo home-exists || echo home-missing")
        visible = [line for line in out["output"].splitlines() if line.startswith("VISIBLE") and not line.endswith(": ")]
        check("host", not visible,
              "; ".join(visible) if visible else f"not visible inside: {', '.join(paths)}")
        home_line = [line for line in out["output"].splitlines() if line.startswith(("HOME=", "home-"))]
        print(f"info  home      {' '.join(home_line)} (--no-home: the home path is set but not mounted)")

        times = []
        for _ in range(5):
            t = time.time()
            run("true")
            times.append(time.time() - t)
        times.sort()
        check("overhead", times[2] < 3.0, f"median {times[2]:.2f} s per command")

        out = run("curl -sS -m 5 -o /dev/null -w '%{http_code}' https://pypi.org 2>&1 || echo no-network")
        print(f"info  network   from inside the container: {' '.join(out['output'].split())[:80]} "
              "(login nodes have internet, compute nodes do not)")

        if args.try_limits:
            cmd = env._exec_command("nproc")
            cmd[cmd.index("exec") + 1:cmd.index("exec") + 1] = ["--cpus", "2", "--memory", "4G"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            msg = (res.stdout.strip() + " " + res.stderr.strip()).strip()
            print(f"info  limits    --cpus 2 --memory 4G: exit {res.returncode}: {msg[:200]}")
    finally:
        env.cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print("\nAll checks passed." if not failed else f"\nFailed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
